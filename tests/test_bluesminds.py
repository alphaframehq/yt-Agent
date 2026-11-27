"""Tests for the Bluesminds paid LLM fallback (27 Nov 2026).

Bluesminds (https://api.bluesminds.com/v1, OpenAI-compatible) sits in
the fallback chain AFTER the free Gemini pool and BEFORE OpenRouter:
Gemini stays primary because it is free; the $100 Bluesminds balance
is spent only when the free tier is exhausted. TEXT ONLY — no TTS or
image model is routed through here.

No network, no real keys: httpx and time.sleep are faked, and every
test uses an obviously fake key (``sk-test-fake-...``). The key value
must never appear in a log line — pinned by
``test_key_never_appears_in_logs``.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from loguru import logger

from config_manager import DEFAULT_CONFIG
from tools.bluesminds import (
    BLUESMINDS_DEFAULT_MODEL,
    BluesmindsError,
    call_bluesminds,
    get_bluesminds_model,
)
from tools.llm_provider import LLMProvider

FAKE_KEY = "sk-test-fake-bluesminds-key-0000"


class _FakeResp:
    """Minimal httpx-like response with controllable status/headers/body."""

    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.request = httpx.Request(
            "POST", "https://api.bluesminds.com/v1/chat/completions"
        )
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def _chat_body(text):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": 10},
    }


def _client_class(responses, calls):
    """httpx.Client stand-in popping canned responses, recording calls."""

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": dict(headers or {}), "json": json})
            return responses.pop(0)

    return _Client


class TestBluesmindsClient(unittest.TestCase):
    def _run(self, responses, env_extra=None):
        """Call with canned responses; return (result_or_exc, calls, sleeps)."""
        calls = []
        env = {"BLUESMINDS_API_KEY": FAKE_KEY}
        if env_extra:
            env.update(env_extra)
        # An ambient BLUESMINDS_MODEL on the dev machine must not
        # leak into the default-model assertions.
        saved_model = os.environ.pop("BLUESMINDS_MODEL", None)
        try:
            with (
                patch.dict(os.environ, env, clear=False),
                patch("tools.bluesminds.httpx.Client", _client_class(responses, calls)),
                patch("tools.bluesminds.time.sleep") as mock_sleep,
            ):
                try:
                    result = call_bluesminds("sys", "user")
                    return result, calls, mock_sleep
                except Exception as exc:  # noqa: BLE001
                    return exc, calls, mock_sleep
        finally:
            if saved_model is not None:
                os.environ["BLUESMINDS_MODEL"] = saved_model

    def test_success_returns_text(self):
        result, calls, _ = self._run([_FakeResp(200, _chat_body("hello world"))])
        self.assertEqual(result, "hello world")
        self.assertEqual(len(calls), 1)
        self.assertIn("api.bluesminds.com/v1/chat/completions", calls[0]["url"])
        self.assertEqual(
            calls[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}"
        )

    def test_default_model_is_kimi_k3(self):
        self.assertEqual(BLUESMINDS_DEFAULT_MODEL, "moonshotai/kimi-k3")
        with patch.dict(os.environ, {}, clear=True):
            # No BLUESMINDS_MODEL set: the default applies.
            self.assertEqual(get_bluesminds_model(), "moonshotai/kimi-k3")
        _, calls, _ = self._run([_FakeResp(200, _chat_body("ok"))])
        self.assertEqual(calls[0]["json"]["model"], "moonshotai/kimi-k3")

    def test_env_model_override(self):
        _, calls, _ = self._run(
            [_FakeResp(200, _chat_body("ok"))],
            env_extra={"BLUESMINDS_MODEL": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"},
        )
        self.assertEqual(
            calls[0]["json"]["model"],
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        )

    def test_429_with_retry_after_retries_then_succeeds(self):
        result, calls, mock_sleep = self._run(
            [
                _FakeResp(429, headers={"Retry-After": "1"}),
                _FakeResp(200, _chat_body("after retry")),
            ]
        )
        self.assertEqual(result, "after retry")
        self.assertEqual(len(calls), 2)
        mock_sleep.assert_called_once_with(1.0)

    def test_429_without_retry_after_uses_backoff(self):
        result, _, mock_sleep = self._run(
            [_FakeResp(429), _FakeResp(200, _chat_body("ok"))]
        )
        self.assertEqual(result, "ok")
        mock_sleep.assert_called_once_with(2.0)

    def test_429_exhausted_raises(self):
        result, calls, mock_sleep = self._run(
            [_FakeResp(429), _FakeResp(429), _FakeResp(429)]
        )
        # BluesmindsError (RuntimeError), NOT an httpx error — so the
        # tenacity retry on LLMProvider.call does not re-run the
        # client's own 3 attempts and burn paid calls.
        self.assertIsInstance(result, BluesmindsError)
        self.assertEqual(len(calls), 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_500_retries_then_succeeds(self):
        result, calls, _ = self._run(
            [_FakeResp(500), _FakeResp(200, _chat_body("recovered"))]
        )
        self.assertEqual(result, "recovered")
        self.assertEqual(len(calls), 2)

    def test_retry_after_capped_at_sixty(self):
        result, _, mock_sleep = self._run(
            [
                _FakeResp(429, headers={"Retry-After": "300"}),
                _FakeResp(200, _chat_body("ok")),
            ]
        )
        self.assertEqual(result, "ok")
        mock_sleep.assert_called_once_with(60.0)

    def test_401_raises_immediately_no_retry(self):
        result, calls, mock_sleep = self._run([_FakeResp(401)])
        self.assertIsInstance(result, BluesmindsError)
        self.assertEqual(len(calls), 1)
        mock_sleep.assert_not_called()

    def test_network_error_retries_then_raises(self):
        """A transport failure is retryable like a 5xx, then fails
        loudly as BluesmindsError (never silent, never an httpx
        error that tenacity would re-run)."""
        calls = []

        class _FlakyClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                calls.append(1)
                raise httpx.ConnectError("network down")

        with (
            patch.dict(os.environ, {"BLUESMINDS_API_KEY": FAKE_KEY}, clear=False),
            patch("tools.bluesminds.httpx.Client", _FlakyClient),
            patch("tools.bluesminds.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(BluesmindsError):
                call_bluesminds("sys", "user")
        self.assertEqual(len(calls), 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_missing_key_raises_value_error(self):
        env = dict(os.environ)
        env.pop("BLUESMINDS_API_KEY", None)
        env.pop("BLUESMINDS_MODEL", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as ctx:
                call_bluesminds("sys", "user")
        self.assertIn("BLUESMINDS_API_KEY", str(ctx.exception))

    def test_empty_content_raises(self):
        result, _, _ = self._run(
            [_FakeResp(200, {"choices": [{"message": {"content": None}}]})]
        )
        self.assertIsInstance(result, ValueError)
        self.assertIn("no content", str(result).lower())

    def test_api_error_field_raises(self):
        result, _, _ = self._run(
            [_FakeResp(200, {"error": {"message": "insufficient balance"}})]
        )
        self.assertIsInstance(result, ValueError)
        self.assertIn("insufficient balance", str(result))

    def test_key_never_appears_in_logs(self):
        """The key value must never be written to any log line —
        success, retry, or failure paths."""
        captured = []
        handler_id = logger.add(captured.append, format="{message}", level="DEBUG")
        try:
            self._run(
                [
                    _FakeResp(429, headers={"Retry-After": "1"}),
                    _FakeResp(200, _chat_body("ok")),
                ]
            )
            self._run([_FakeResp(401)])
            self._run(
                [_FakeResp(200, {"choices": [{"message": {"content": ""}}]})]
            )
        finally:
            logger.remove(handler_id)
        blob = "\n".join(str(m) for m in captured)
        self.assertTrue(blob, "expected log lines to be captured")
        self.assertNotIn(FAKE_KEY, blob)
        # Not even a recognisable fragment of the key.
        self.assertNotIn("sk-test-fake", blob)


class TestBluesmindsProviderWiring(unittest.TestCase):
    def test_llm_provider_dispatch(self):
        with patch(
            "tools.llm_provider.call_bluesminds", return_value="structured text"
        ) as mock_call:
            resp = LLMProvider("bluesminds", "moonshotai/kimi-k3").call(
                "sys", "user"
            )
        mock_call.assert_called_once()
        self.assertEqual(resp.content, "structured text")
        self.assertEqual(resp.provider, "bluesminds")
        self.assertEqual(resp.model, "moonshotai/kimi-k3")

    def test_env_model_overrides_chain_model(self):
        with (
            patch.dict(
                os.environ,
                {"BLUESMINDS_MODEL": "z-ai/glm-5.3-flash"},
                clear=False,
            ),
            patch(
                "tools.llm_provider.call_bluesminds", return_value="ok"
            ) as mock_call,
        ):
            resp = LLMProvider("bluesminds", "moonshotai/kimi-k3").call(
                "sys", "user"
            )
        self.assertEqual(resp.model, "z-ai/glm-5.3-flash")
        self.assertEqual(mock_call.call_args.kwargs["model"], "z-ai/glm-5.3-flash")

    def test_provider_failure_is_not_retried_by_tenacity(self):
        """A BluesmindsError must reach call_with_fallback after
        exactly ONE provider attempt — tenacity retries httpx
        errors, and a re-run here would burn paid calls."""
        with patch(
            "tools.llm_provider.call_bluesminds",
            side_effect=BluesmindsError("Bluesminds HTTP 429"),
        ) as mock_call:
            with self.assertRaises(BluesmindsError):
                LLMProvider("bluesminds", "moonshotai/kimi-k3").call(
                    "sys", "user"
                )
        self.assertEqual(mock_call.call_count, 1)

    def test_chain_order_gemini_then_bluesminds_then_openrouter(self):
        chain = DEFAULT_CONFIG["verify_chain"]
        self.assertEqual(
            chain,
            [
                ["gemini", "gemini-3.8-flash"],
                ["gemini", "gemini-3.5-flash-lite"],
                ["bluesminds", "moonshotai/kimi-k3"],
                ["openrouter", "openrouter/free"],
            ],
        )

    def test_fallback_reaches_bluesminds_after_gemini_fails(self):
        """call_with_fallback walks the chain: a failing gemini step
        is followed by a bluesminds step (mocked), not skipped."""
        from tools.llm_provider import call_with_fallback

        calls = []

        def _fake_call(self, system_prompt, user_prompt, **kwargs):
            calls.append(self.provider)
            if self.provider == "gemini":
                raise RuntimeError("gemini down")
            return MagicMock(content="from bluesminds")

        with patch.object(LLMProvider, "call", _fake_call):
            result = call_with_fallback(
                [
                    ["gemini", "gemini-3.8-flash"],
                    ["bluesminds", "moonshotai/kimi-k3"],
                ],
                "sys",
                "user",
            )
        self.assertEqual(calls, ["gemini", "bluesminds"])
        self.assertEqual(result.content, "from bluesminds")


if __name__ == "__main__":
    unittest.main()
