"""TubePilot AI — Gemini TTS unit tests (Phase 2, worker 4).

All Gemini API calls are mocked (no live key, no network): the
httpx.Client used by tools/gemini_tts.py is replaced with a fake
that returns canned generateContent responses carrying base64 PCM.

Covers:
1. Success writes a real WAV; duration is measured from frames.
2. Raises (fail-loud) with no GEMINI_KEY, on HTTP errors, on empty /
   safety-blocked responses, and on empty text — never stub audio.
3. Polly voice names map to Gemini prebuilt voices in the request.
4. Cache hit does not call the API a second time.
5. Key-pool failover: a 429 on the first key tries the next key.
6. agents/tts.py: Gemini is the default; Polly is tried once on
   Gemini failure only when AWS keys exist; otherwise the original
   Gemini error is raised.
"""

import base64
import os
import struct
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

import tools.gemini_tts as gemini_tts
from tools.gemini_tts import resolve_voice, synthesize_speech
from tools.secrets import reset_gemini_pool

_FAKE_KEY = "AIzaFakeTestKey1234567890"
_FAKE_KEY_2 = "AIzaFakeTestKey0987654321"


def _pcm_bytes(seconds: float, rate: int = 24000) -> bytes:
    """Silence PCM: 16-bit mono samples (test fixture only)."""
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *([0] * n))


def _tts_response(pcm: bytes, rate: int = 24000) -> dict:
    return {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{
                "inlineData": {
                    "mimeType": f"audio/L16;codec=audio/pcm;rate={rate}",
                    "data": base64.b64encode(pcm).decode(),
                }
            }]},
        }]
    }


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client; records posts, serves canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "json": json})
        if not self._responses:
            raise AssertionError("FakeClient: no more canned responses")
        return self._responses.pop(0)


class TestGeminiTTS(unittest.TestCase):
    def setUp(self):
        reset_gemini_pool()
        os.environ["GEMINI_KEY"] = _FAKE_KEY
        for name in ("GEMINI_KEY_POOL", "GEMINI_KEY_1", "GEMINI_KEY_2",
                     "GEMINI_TTS_VOICE", "GEMINI_TTS_MODEL", "TTS_PROVIDER"):
            os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_dir = Path(self._tmp.name) / "audio"
        self._cache_dir.mkdir(parents=True)
        self._patch_cache = patch.object(gemini_tts, "CACHE_DIR", self._cache_dir)
        self._patch_cache.start()

    def tearDown(self):
        self._patch_cache.stop()
        self._tmp.cleanup()
        os.environ.pop("GEMINI_KEY", None)
        os.environ.pop("GEMINI_KEY_1", None)
        os.environ.pop("GEMINI_KEY_2", None)
        reset_gemini_pool()

    def _run_with(self, responses, text="Hello world, this is a test."):
        client = _FakeClient(responses)
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            result = synthesize_speech(text)
        return result, client

    def test_success_writes_real_wav_with_measured_duration(self):
        """A 2.0s PCM payload becomes a WAV whose measured length is 2.0s."""
        result, client = self._run_with(
            [_FakeResponse(200, _tts_response(_pcm_bytes(2.0)))],
            text="Hello world.",
        )
        path, duration = result
        self.assertEqual(duration, 2.0)
        self.assertTrue(Path(path).suffix == ".wav")
        self.assertTrue(Path(path).stat().st_size > 0)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24000)
        # Model + voice in the request URL / payload.
        self.assertIn("gemini-2.5-flash-preview-tts", client.posts[0]["url"])
        self.assertEqual(
            client.posts[0]["json"]["generationConfig"]["responseModalities"],
            ["AUDIO"],
        )

    def test_voice_mapping_polly_names_to_gemini(self):
        self.assertEqual(resolve_voice("Joanna"), "Kore")
        self.assertEqual(resolve_voice("Matthew"), "Charon")
        self.assertEqual(resolve_voice("Kajal"), "Aoede")
        # A Gemini voice name passes through unchanged.
        self.assertEqual(resolve_voice("Fenrir"), "Fenrir")
        _, client = self._run_with(
            [_FakeResponse(200, _tts_response(_pcm_bytes(1.0)))],
            text="Voice mapping check.",
        )
        voice_cfg = client.posts[0]["json"]["generationConfig"]["speechConfig"]
        self.assertEqual(
            voice_cfg["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Kore"
        )

    def test_cache_hit_does_not_call_api_again(self):
        _, client1 = self._run_with(
            [_FakeResponse(200, _tts_response(_pcm_bytes(1.5)))],
            text="Cached narration.",
        )
        self.assertEqual(len(client1.posts), 1)
        client2 = _FakeClient([_FakeResponse(500, text="must not be called")])
        with patch.object(gemini_tts.httpx, "Client", return_value=client2):
            path, duration = synthesize_speech("Cached narration.")
        self.assertEqual(duration, 1.5)
        self.assertEqual(len(client2.posts), 0)

    def test_raises_without_gemini_key(self):
        os.environ.pop("GEMINI_KEY", None)
        reset_gemini_pool()
        with self.assertRaises(RuntimeError):
            synthesize_speech("No key configured.")

    def test_raises_on_http_error(self):
        client = _FakeClient([_FakeResponse(400, text="bad request: bad model")])
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                synthesize_speech("Bad request test.")

    def test_raises_on_empty_candidates(self):
        client = _FakeClient([_FakeResponse(200, {"candidates": []})])
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                synthesize_speech("Empty response test.")

    def test_raises_on_safety_block(self):
        payload = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        client = _FakeClient([_FakeResponse(200, payload)])
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                synthesize_speech("Blocked text test.")

    def test_raises_on_missing_audio_part(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "no audio"}]}}]}
        client = _FakeClient([_FakeResponse(200, payload)])
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            with self.assertRaises(RuntimeError):
                synthesize_speech("No audio part test.")

    def test_raises_on_empty_text(self):
        with self.assertRaises(ValueError):
            synthesize_speech("   ")

    def test_pool_failover_on_429(self):
        """A 429 on GEMINI_KEY retries with GEMINI_KEY_1 and succeeds."""
        os.environ["GEMINI_KEY_1"] = _FAKE_KEY_2
        reset_gemini_pool()
        client = _FakeClient([
            _FakeResponse(429, text="rate limited"),
            _FakeResponse(200, _tts_response(_pcm_bytes(1.0))),
        ])
        with patch.object(gemini_tts.httpx, "Client", return_value=client):
            path, duration = synthesize_speech("Failover test.")
        self.assertEqual(duration, 1.0)
        self.assertEqual(len(client.posts), 2)
        self.assertIn(_FAKE_KEY, client.posts[0]["url"])
        self.assertIn(_FAKE_KEY_2, client.posts[1]["url"])


class TestTTSAgentWiring(unittest.TestCase):
    """agents/tts.py: Gemini default, Polly fallback only with AWS keys."""

    def setUp(self):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "TTS_PROVIDER"):
            os.environ.pop(name, None)

    def tearDown(self):
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "TTS_PROVIDER"):
            os.environ.pop(name, None)

    def test_gemini_is_default_provider(self):
        from agents.tts import synthesize_scene_audio
        with patch("agents.tts.synthesize_speech_gemini",
                   return_value=("/tmp/fake.wav", 3.5)) as mock_gemini, \
             patch("agents.tts.synthesize_speech_polly") as mock_polly:
            path, duration = synthesize_scene_audio("Hello.", voice="Joanna")
        self.assertEqual((path, duration), ("/tmp/fake.wav", 3.5))
        mock_gemini.assert_called_once()
        mock_polly.assert_not_called()

    def test_gemini_failure_without_aws_keys_raises_original_error(self):
        from agents.tts import synthesize_scene_audio
        gemini_err = RuntimeError("Gemini TTS request failed with HTTP 500")
        with patch("agents.tts.synthesize_speech_gemini", side_effect=gemini_err), \
             patch("agents.tts.synthesize_speech_polly") as mock_polly:
            with self.assertRaises(RuntimeError) as ctx:
                synthesize_scene_audio("Hello.")
        self.assertIs(ctx.exception, gemini_err)
        mock_polly.assert_not_called()

    def test_polly_fallback_when_aws_keys_exist(self):
        from agents.tts import synthesize_scene_audio
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAFAKEKEY123456"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "fake-secret-1234567890"
        with patch("agents.tts.synthesize_speech_gemini",
                   side_effect=RuntimeError("Gemini TTS down")), \
             patch("agents.tts.synthesize_speech_polly",
                   return_value=("/tmp/polly.mp3", 2.0)) as mock_polly:
            path, duration = synthesize_scene_audio("Hello.")
        self.assertEqual((path, duration), ("/tmp/polly.mp3", 2.0))
        mock_polly.assert_called_once()

    def test_polly_only_mode(self):
        from agents.tts import synthesize_scene_audio
        os.environ["TTS_PROVIDER"] = "polly"
        with patch("agents.tts.synthesize_speech_gemini") as mock_gemini, \
             patch("agents.tts.synthesize_speech_polly",
                   return_value=("/tmp/polly.mp3", 1.0)) as mock_polly:
            path, duration = synthesize_scene_audio("Hello.")
        self.assertEqual((path, duration), ("/tmp/polly.mp3", 1.0))
        mock_gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
