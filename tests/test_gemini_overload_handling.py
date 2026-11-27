"""Tests for the Gemini free-tier overload handling fix (27 Nov 2026).

User's question: "Gemini baar-baar fail kyun ho ja raha hai? Model
sahi nahi hai?" Answer, verified 26 Nov 2026 against Google's own
pricing page (ai.google.dev/gemini-api/docs/pricing, crawled
2026-11-26 19:45 UTC): the text model IDs in the repo are CORRECT and
free — gemini-3.8-flash and gemini-3.5-flash-lite both list "Free of
charge" for input AND output; gemini-2.5-flash-preview-tts (TTS) is
free too and synthesized all 8 scenes live in run #18. The 503s are
free-tier capacity overload on the most-used free model, not wrong
IDs. Only the image model (imagen-3.0-generate-001) is paid-only and
404s, and that path already falls back to Pixabay (commit 99b7e29).

Fix under test (tools/llm_provider.py):
1. Retry-After is honored ONCE (capped at 60s) with a single re-run
   of the key pass when the server sends it on a 429/5xx.
2. All-keys 429 (and mixed 429+5xx) raise GeminiOverloadedError so
   the chain moves to the next provider promptly instead of tenacity
   re-running the same overloaded model 3x.
3. 503 without Retry-After still raises immediately, one POST per key
   (the run #17 guarantee, re-pinned here).
4. The configured model IDs are all on the verified-free list, and
   the chain runs 3.8-flash -> 3.5-flash-lite -> bluesminds/kimi-k3
   (paid fallback, 27 Nov 2026) -> openrouter/free.
   (27 Nov 2026, run #20: gemini-2.5-flash-lite was REMOVED from the
   chain — the model is retired and returns HTTP 404.)

No network, no real keys: httpx and time.sleep are faked.
"""

import sys
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config_manager import DEFAULT_CONFIG
from tools.gemini_tts import DEFAULT_MODEL as TTS_MODEL
from tools.llm_provider import (
    FREE_TIER_TEXT_MODELS,
    FREE_TIER_TTS_MODELS,
    RETRY_AFTER_MAX_WAIT_SEC,
    GeminiOverloadedError,
    LLMProvider,
    _parse_retry_after,
)


class _FakeResp:
    """Minimal httpx-like response with controllable status/headers."""

    def __init__(self, status_code, headers=None, text='{"ok": true}'):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.request = httpx.Request("POST", "https://example.com")
        self._text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request, response=self
            )

    def json(self):
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": self._text}]},
                    "finishReason": "STOP",
                }
            ]
        }


def _client_class(responses):
    """httpx.Client stand-in popping canned responses in order."""

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return responses.pop(0)

    return _Client


class TestParseRetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(
            _parse_retry_after(_FakeResp(503, {"Retry-After": "25"})), 25.0
        )

    def test_capped_at_sixty(self):
        self.assertEqual(
            _parse_retry_after(_FakeResp(503, {"Retry-After": "300"})),
            RETRY_AFTER_MAX_WAIT_SEC,
        )
        self.assertEqual(RETRY_AFTER_MAX_WAIT_SEC, 60.0)

    def test_missing_header_is_zero(self):
        self.assertEqual(_parse_retry_after(_FakeResp(503)), 0.0)

    def test_garbage_header_is_zero(self):
        self.assertEqual(
            _parse_retry_after(_FakeResp(503, {"Retry-After": "soon"})), 0.0
        )

    def test_mock_headers_are_zero(self):
        # A MagicMock response (as in older tests) must never produce
        # a sleep: only real str/int/float header values count.
        resp = MagicMock()
        resp.headers = MagicMock()
        self.assertEqual(_parse_retry_after(resp), 0.0)

    def test_no_headers_attribute_is_zero(self):
        class _Bare:
            status_code = 503

        self.assertEqual(_parse_retry_after(_Bare()), 0.0)

    def test_http_date_header(self):
        future = formatdate(time.time() + 45, usegmt=True)
        wait = _parse_retry_after(_FakeResp(503, {"Retry-After": future}))
        self.assertGreater(wait, 30.0)
        self.assertLessEqual(wait, 60.0)


class TestGeminiOverloadHandling(unittest.TestCase):
    def _run_call(self, keys, responses):
        """Call gemini provider with canned responses; return (result, posts, sleeps)."""
        posts = {"n": 0}

        class _CountingClient(_client_class(responses)):
            def post(self, *a, **k):
                posts["n"] += 1
                return super().post(*a, **k)

        with (
            patch(
                "tools.llm_provider.get_gemini_pool_keys", return_value=keys
            ),
            patch("tools.llm_provider.mark_gemini_key_failed", return_value=None),
            patch("tools.llm_provider.httpx.Client", _CountingClient),
            patch("time.sleep") as mock_sleep,
        ):
            try:
                result = LLMProvider("gemini", "gemini-3.8-flash").call(
                    "sys", "user"
                )
                return result, posts["n"], mock_sleep
            except Exception as exc:
                return exc, posts["n"], mock_sleep

    def test_503_with_retry_after_waits_once_then_succeeds(self):
        """Server says wait 5s: sleep exactly once, re-run the key
        pass, and succeed — instead of abandoning gemini-3.8-flash
        for the fallback chain on a transient overload."""
        responses = [
            _FakeResp(503, {"Retry-After": "5"}),
            _FakeResp(200),
        ]
        result, n_posts, mock_sleep = self._run_call(["k1"], responses)
        self.assertEqual(result.content, '{"ok": true}')
        self.assertEqual(n_posts, 2)
        mock_sleep.assert_called_once_with(5.0)

    def test_503_retry_after_capped_at_sixty(self):
        responses = [
            _FakeResp(503, {"Retry-After": "300"}),
            _FakeResp(200),
        ]
        result, n_posts, mock_sleep = self._run_call(["k1"], responses)
        self.assertEqual(result.content, '{"ok": true}')
        mock_sleep.assert_called_once_with(60.0)

    def test_503_without_retry_after_raises_immediately(self):
        """Run #17 guarantee, re-pinned: no Retry-After header means
        no second pass and no tenacity retry — one POST per key."""
        responses = [_FakeResp(503), _FakeResp(503)]
        result, n_posts, mock_sleep = self._run_call(["k1", "k2"], responses)
        self.assertIsInstance(result, GeminiOverloadedError)
        self.assertIn("503", str(result))
        self.assertEqual(n_posts, 2)
        mock_sleep.assert_not_called()

    def test_all_keys_429_raises_overload_without_tenacity_retry(self):
        """A quota-exhausted key must move the chain on promptly,
        not burn tenacity's 3x retry on the same model."""
        responses = [_FakeResp(429), _FakeResp(429)]
        result, n_posts, mock_sleep = self._run_call(["k1", "k2"], responses)
        self.assertIsInstance(result, GeminiOverloadedError)
        self.assertIn("429", str(result))
        self.assertEqual(n_posts, 2)
        mock_sleep.assert_not_called()

    def test_mixed_429_and_503_raises_overload(self):
        """Multi-key pool, one key 429 and one 503: the model is
        unusable this call — fail fast to the next provider."""
        responses = [_FakeResp(429), _FakeResp(503)]
        result, n_posts, mock_sleep = self._run_call(["k1", "k2"], responses)
        self.assertIsInstance(result, GeminiOverloadedError)
        self.assertEqual(n_posts, 2)
        mock_sleep.assert_not_called()

    def test_429_with_retry_after_waits_once_then_succeeds(self):
        responses = [
            _FakeResp(429, {"Retry-After": "10"}),
            _FakeResp(200),
        ]
        result, n_posts, mock_sleep = self._run_call(["k1"], responses)
        self.assertEqual(result.content, '{"ok": true}')
        mock_sleep.assert_called_once_with(10.0)


class TestConfiguredModelIdsAreVerifiedFree(unittest.TestCase):
    def test_text_chain_uses_only_verified_free_ids(self):
        chain = DEFAULT_CONFIG["verify_chain"]
        gemini_models = [m for p, m in chain if p == "gemini"]
        self.assertTrue(gemini_models, "chain must contain gemini models")
        for model in gemini_models:
            self.assertIn(
                model,
                FREE_TIER_TEXT_MODELS,
                f"{model} is not on the verified free-tier list",
            )

    def test_chain_order_strongest_first_lightest_last(self):
        chain = [m for _p, m in DEFAULT_CONFIG["verify_chain"]]
        self.assertEqual(
            chain,
            [
                "gemini-3.8-flash",
                "gemini-3.5-flash-lite",
                # Paid Bluesminds fallback sits after the free Gemini
                # pool and before the free OpenRouter tail (27 Nov 2026).
                "moonshotai/kimi-k3",
                "openrouter/free",
            ],
        )

    def test_active_text_models_are_verified_free(self):
        for key in ("script", "verify"):
            model = DEFAULT_CONFIG["active_models"][key]
            _provider, _sep, name = model.partition("/")
            self.assertIn(name, FREE_TIER_TEXT_MODELS)

    def test_tts_model_is_verified_free(self):
        self.assertIn(TTS_MODEL, FREE_TIER_TTS_MODELS)

    def test_image_model_is_not_free(self):
        # Imagen 3 is paid-only — it must never be treated as a free
        # text/TTS model; its path is the Pixabay fallback (99b7e29).
        image_model = DEFAULT_CONFIG["active_models"]["image"]
        _provider, _sep, name = image_model.partition("/")
        self.assertNotIn(name, FREE_TIER_TEXT_MODELS)
        self.assertNotIn(name, FREE_TIER_TTS_MODELS)


if __name__ == "__main__":
    unittest.main()
