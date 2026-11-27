# Bluesminds API client (27 Nov 2026) — paid LLM fallback after Gemini.
# OpenAI-compatible chat-completions endpoint at
# https://api.bluesminds.com/v1 (verified live 27 Nov 2026: the
# /v1/models list returns 27 models; chat completion with
# nvidia/ising-calibration-1.5-31b returned a real reply).
#
# TEXT ONLY (research / script / verify). Bluesminds has no TTS model
# (Kokoro stays the TTS fallback) and image generation via
# diffusiongemma is out of scope here.
#
# Cost guard: Gemini is free and stays primary; Bluesminds is only
# reached after the whole Gemini key pool is exhausted, so the
# $100 balance is spent only when the free tier is down. The key is
# read ONLY from the env var BLUESMINDS_API_KEY at runtime — it is
# never written to a file, a log, or a test fixture. Logs carry the
# model name + HTTP status only, never the key (see the no-key-leak
# test in tests/test_bluesminds.py).
"""Minimal OpenAI-compatible chat client for the Bluesminds API."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from loguru import logger

from tools.secrets import _get_secret_optional

__all__ = [
    "BLUESMINDS_BASE_URL",
    "BLUESMINDS_DEFAULT_MODEL",
    "BluesmindsError",
    "call_bluesminds",
    "get_bluesminds_model",
]

BLUESMINDS_BASE_URL = "https://api.bluesminds.com/v1"

# Default model (27 Nov 2026): moonshotai/kimi-k3 — long-context,
# strong at research/script writing. Override with the env var
# BLUESMINDS_MODEL; other verified options on the account include
# nvidia/nemotron-3-nano-omni-30b-a3b-reasoning and
# nvidia/nemotron-3.5-lightning-30b-a3b.
BLUESMINDS_DEFAULT_MODEL = "moonshotai/kimi-k3"

# Retry policy for transient failures (429 rate-limit, 5xx server
# errors): at most this many attempts, exponential backoff between
# them when the server sends no Retry-After.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 2.0
# Longest we ever honor a server-sent Retry-After before giving up
# on the call (mirrors RETRY_AFTER_MAX_WAIT_SEC in llm_provider.py).
RETRY_AFTER_MAX_WAIT_SEC = 60.0

# HTTP statuses worth one more attempt. 4xx other than 429 (bad
# request, bad key, unknown model) is final — retrying burns balance.
_RETRYABLE_STATUSES = frozenset((429, 500, 502, 503, 504))


class BluesmindsError(RuntimeError):
    """Raised when a Bluesminds call fails (missing key aside).

    Deliberately a RuntimeError, NOT an httpx error: ``LLMProvider.call``
    retries httpx errors 3x with tenacity, which would re-run this
    client's own 3 attempts and burn up to 9 paid calls on one
    failing request. Raising this instead moves the fallback chain to
    the next provider promptly (same pattern as GeminiOverloadedError
    in llm_provider.py).
    """


def get_bluesminds_model() -> str:
    """Model to use: env ``BLUESMINDS_MODEL`` or the kimi-k3 default."""
    return os.environ.get("BLUESMINDS_MODEL") or BLUESMINDS_DEFAULT_MODEL


def _parse_retry_after(resp: Any) -> float:
    """Seconds to wait from a response's ``Retry-After`` header.

    Accepts a delay in seconds (``"30"``) or an HTTP-date. Returns 0
    when the header is absent, unparsable, or in the past. Capped at
    ``RETRY_AFTER_MAX_WAIT_SEC``. (Same semantics as
    ``tools.llm_provider._parse_retry_after``; kept local so this
    module does not import the provider module — llm_provider imports
    this module, and an import cycle would break both.)
    """
    try:
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    except Exception:
        return 0.0
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        wait = float(raw)
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return 0.0
        try:
            wait = float(raw)
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return 0.0
            if when is None:
                return 0.0
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            wait = (when - datetime.now(timezone.utc)).total_seconds()
    else:
        return 0.0
    if wait <= 0:
        return 0.0
    return min(wait, RETRY_AFTER_MAX_WAIT_SEC)


def call_bluesminds(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
    timeout: float = 60.0,
    max_attempts: int = MAX_ATTEMPTS,
) -> str:
    """Call Bluesminds chat completions and return the plain text reply.

    Args:
        system_prompt: System instructions for the model.
        user_prompt: The user message.
        model: Model id; ``None`` means the env/default model
            (``BLUESMINDS_MODEL`` or ``moonshotai/kimi-k3``).
        temperature: Sampling temperature.
        timeout: Per-request HTTP timeout in seconds.
        max_attempts: Total attempts on 429/5xx (default 3).

    Returns:
        The assistant message text (stripped, non-empty).

    Raises:
        ValueError: When ``BLUESMINDS_API_KEY`` is not set, the API
            returns an error payload, or the reply has no usable
            content. When the key is missing this raises instead of
            returning a fake success — a missing key must move the
            fallback chain on, not silently pass an empty script.
        BluesmindsError: When the HTTP call fails (non-retryable
            status, or retries exhausted on 429/5xx, or a network
            error). A RuntimeError on purpose — see the class docstring.
    """
    api_key = _get_secret_optional("BLUESMINDS_API_KEY")
    if not api_key:
        logger.error(
            "BLUESMINDS_API_KEY not configured. Raising instead of "
            "returning a mock response."
        )
        raise ValueError(
            "BLUESMINDS_API_KEY is required for the Bluesminds provider "
            "(set BLUESMINDS_API_KEY as a GitHub repo secret)"
        )

    model = model or get_bluesminds_model()
    url = f"{BLUESMINDS_BASE_URL}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }

    with httpx.Client(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            # The Authorization header is built inline per attempt so
            # the key value never sits in a shared/logged dict.
            try:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.RequestError as exc:
                # Network failure: retryable like a 5xx, same backoff.
                if attempt < max_attempts:
                    wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                    logger.warning(
                        f"Bluesminds network error (model={model}); "
                        f"retrying in {wait:.0f}s "
                        f"(attempt {attempt}/{max_attempts})."
                    )
                    time.sleep(wait)
                    continue
                raise BluesmindsError(
                    f"Bluesminds network error after {max_attempts} "
                    f"attempts (model={model}): {exc}"
                ) from exc
            if resp.status_code == 200:
                data = resp.json()
                api_error = data.get("error") if isinstance(data, dict) else None
                if api_error:
                    raise ValueError(
                        f"Bluesminds API error (model={model}): {api_error}"
                    )
                choices = data.get("choices") or []
                content = None
                if choices:
                    content = (choices[0].get("message") or {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError(
                        "Bluesminds returned no content (content=None "
                        f"or empty, model={model}); treating as a failed "
                        "attempt."
                    )
                return content.strip()

            # Retryable: 429 or 5xx — wait, then try again.
            if resp.status_code in _RETRYABLE_STATUSES and attempt < max_attempts:
                wait = _parse_retry_after(resp)
                if wait <= 0:
                    wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                logger.warning(
                    f"Bluesminds HTTP {resp.status_code} (model={model}); "
                    f"retrying in {wait:.0f}s "
                    f"(attempt {attempt}/{max_attempts})."
                )
                time.sleep(wait)
                continue

            # Final failure: log model + status only — never the key.
            # BluesmindsError (not HTTPStatusError) so the tenacity
            # retry on LLMProvider.call does not re-run this client's
            # own attempts.
            logger.warning(
                f"Bluesminds call failed (model={model}, "
                f"status={resp.status_code}, attempt {attempt}/{max_attempts})."
            )
            raise BluesmindsError(
                f"Bluesminds HTTP {resp.status_code} (model={model})"
            )

    raise RuntimeError(
        f"Bluesminds call failed without a response (model={model})."
    )
