# PRODUCTION PATCH (local only, 2026-11-25) — yt-Agent audit P0 #1.
# _call_gemini() now RAISES when no GEMINI_KEY is configured, instead of
# returning a fake "{}" success that silently short-circuited
# call_with_fallback(). _call_openrouter() raise behaviour is unchanged.
# _call_bedrock(): only its misleading "Returning fallback response" log line
# was fixed (it already re-raised); behaviour unchanged.
# Source: alphaframehq/yt-Agent @ c39f42541b8e784dcf41c09583dc57227be07667.
# Applied via PR #1 (branch fix/production-mocks), 2026-11-25. No secrets in this file — env-var placeholders only.
"""Unified LLM Provider abstraction for TubePilot AI.

Supports Bedrock (Claude), Gemini (2.0/2.5 Flash), and OpenRouter (:free).
Provides automatic retries with exponential backoff (tenacity), structured output
validation (Pydantic), schema error re-prompting, and multi-provider fallback chains.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Type, TypeVar
import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from tools.secrets import (
    _get_secret_optional,
    get_gemini_pool_keys,
    mark_gemini_key_failed,
    mask_secret,
)

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "SafetyBlockedError",
    "GeminiOverloadedError",
    "call_with_fallback",
]


class SafetyBlockedError(ValueError):
    """Raised when Gemini refuses a prompt on safety grounds.

    Distinct from a transient/network error on purpose: a safety
    refusal must NEVER be retried, re-prompted, or routed around to
    the next provider in the fallback chain (that would just burn the
    chain trying to get a looser model to answer a blocked prompt).
    Callers treat it as "this topic is unproduceable": mark the topic
    failed/skipped and move on to the next queued topic.

    26 Nov 2026: Gemini returns HTTP 200 with an empty ``candidates``
    list (or a candidate whose ``finishReason`` is ``SAFETY``) and
    ``promptFeedback.blockReason`` set, e.g. ``SAFETY``. The old code
    returned that as a successful empty response (``content=""``),
    which then failed JSON parsing 3x in ``call_structured`` before
    the fallback chain burned the remaining providers on the same
    blocked prompt.
    """


class GeminiOverloadedError(RuntimeError):
    """Raised when every Gemini key in the pool fails with a 5xx
    server error (503 overload, 500/502/504).

    Deliberately a RuntimeError, NOT an httpx error: ``LLMProvider.call``
    retries httpx errors 3x with tenacity, which would burn minutes
    re-trying an overloaded backend that already failed on every key.
    Raising this instead moves the fallback chain to the next provider
    promptly (run #17: Gemini 503s stalled the chain while OpenRouter
    was never tried in time).
    """


class LLMResponse(BaseModel):
    """Normalized response from an LLM call."""
    content: str
    parsed: Optional[Any] = None
    provider: str
    model: str
    tokens_used: int = 0
    latency_ms: int = 0


def _summarize_validation_error(exc: Exception) -> str:
    """Condense a parse/validation failure into the exact fields the
    LLM must fix, one short line per problem.

    The raw pydantic error dump is verbose (nested context, URLs);
    a repair re-prompt carrying it verbatim wastes tokens and the
    model still omits the same fields. This keeps at most 12
    ``field.path: problem`` lines so the next attempt knows precisely
    what was missing or invalid.
    """
    if isinstance(exc, ValidationError):
        lines: list[str] = []
        for err in exc.errors()[:12]:
            loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
            lines.append(f"- {loc}: {err.get('msg', exc)}")
        if len(exc.errors()) > 12:
            lines.append(f"- ... and {len(exc.errors()) - 12} more problems")
        return "\n".join(lines)
    text = str(exc)
    if len(text) > 500:
        text = text[:500] + "…"
    return text


class LLMProvider:
    """Unified interface to query diverse LLM backends with resilience."""

    def __init__(self, provider: str, model: str):
        self.provider = provider.lower()
        self.model = model

    @retry(
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError, TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Type[T]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Call the underlying LLM with retry, backoff, and optional schema enforcement."""
        if self.provider == "gemini":
            return self._call_gemini(system_prompt, user_prompt, schema, temperature)
        elif self.provider == "bedrock":
            return self._call_bedrock(system_prompt, user_prompt, schema, temperature)
        elif self.provider == "openrouter":
            return self._call_openrouter(system_prompt, user_prompt, schema, temperature)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def call_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Type[T],
        max_repair_attempts: int = 2,
    ) -> T:
        """Enforces Pydantic schema output, with error-context re-prompting on validation failure."""
        current_prompt = user_prompt
        last_error = ""

        for attempt in range(max_repair_attempts + 1):
            if attempt > 0:
                logger.warning(
                    f"Schema parsing failed (attempt {attempt}/{max_repair_attempts}). Re-prompting with error context."
                )
                current_prompt = (
                    f"{user_prompt}\n\n"
                    f"PREVIOUS ATTEMPT OUTPUT FAILED SCHEMA VALIDATION:\n"
                    f"Validation Error: {last_error}\n\n"
                    f"Please output ONLY raw valid JSON conforming strictly to the requested schema. No markdown formatting."
                )

            res = self.call(system_prompt, current_prompt, schema=schema)

            raw_text = (res.content or "").strip()
            if not raw_text:
                # Empty provider response: a failed attempt, not a
                # crash — let the fallback chain try the next
                # provider (run #17: OpenRouter content=None blew up
                # as a pydantic error on LLMResponse instead).
                raise ValueError(
                    f"Provider {self.provider}/{self.model} returned "
                    "an empty response (no content)."
                )
            # Clean possible markdown json wrapper
            if raw_text.startswith("```json"):
                raw_text = raw_text[7:]
            if raw_text.startswith("```"):
                raw_text = raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()

            try:
                data = json.loads(raw_text)
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = _summarize_validation_error(e)
                if attempt == max_repair_attempts:
                    raise

        raise RuntimeError("Failed to parse structured LLM output.")

    def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Type[T]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Call Google Gemini API with automatic key pool failover on rate-limits (429) or quota errors."""
        keys = get_gemini_pool_keys()
        if not keys:
            # PRODUCTION PATCH (P0 #1): raise instead of returning a mock "{}".
            # A fake success here made call_with_fallback() never try the next
            # provider, so the whole pipeline ran on an empty LLM response.
            logger.error("No GEMINI_KEY configured. Raising instead of returning a mock response.")
            raise ValueError("GEMINI_KEY is required for Gemini provider (set GEMINI_KEY or GEMINI_KEY_POOL)")

        headers = {"Content-Type": "application/json"}
        payload: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": temperature,
            },
        }

        if schema is not None:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        last_err: Optional[Exception] = None
        server_error_statuses: list[int] = []
        for idx, api_key in enumerate(keys):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={api_key}"
            try:
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code == 429:
                        logger.warning(
                            f"Gemini key [{mask_secret(api_key)}] hit 429 Rate Limit. "
                            f"Failing over to next key in pool ({idx + 1}/{len(keys)})..."
                        )
                        mark_gemini_key_failed(api_key, error_status=429, cooldown_sec=60.0)
                        last_err = httpx.HTTPStatusError("429 Rate Limit", request=resp.request, response=resp)
                        continue
                    elif resp.status_code in (401, 403):
                        logger.warning(
                            f"Gemini key [{mask_secret(api_key)}] failed with HTTP {resp.status_code}. "
                            f"Failing over to next key in pool ({idx + 1}/{len(keys)})..."
                        )
                        mark_gemini_key_failed(api_key, error_status=resp.status_code, cooldown_sec=3600.0)
                        last_err = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                        continue
                    elif resp.status_code in (500, 502, 503, 504):
                        # Overload / server error (run #17: 503s
                        # stalled the chain): fail over to the next
                        # key immediately and record it, so the
                        # post-loop check can skip tenacity's retry
                        # entirely when every key is 5xx.
                        logger.warning(
                            f"Gemini key [{mask_secret(api_key)}] hit HTTP "
                            f"{resp.status_code} (server error/overload). "
                            f"Failing over to next key in pool ({idx + 1}/{len(keys)})..."
                        )
                        mark_gemini_key_failed(api_key, error_status=resp.status_code, cooldown_sec=60.0)
                        server_error_statuses.append(resp.status_code)
                        last_err = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    # -- Safety refusal (26 Nov 2026 fix) ----------------
                    # Gemini answers a blocked prompt with HTTP 200 and
                    # no usable candidate: promptFeedback.blockReason
                    # (e.g. "SAFETY") and/or finishReason "SAFETY".
                    # That is a final refusal, not an empty success.
                    feedback = data.get("promptFeedback") or {}
                    block_reason = feedback.get("blockReason")
                    candidates = data.get("candidates", [])
                    finish_reason = ""
                    if candidates:
                        finish_reason = str(
                            candidates[0].get("finishReason") or ""
                        )
                    if block_reason or finish_reason == "SAFETY":
                        raise SafetyBlockedError(
                            "Gemini safety refusal "
                            f"(blockReason={block_reason or '-'}, "
                            f"finishReason={finish_reason or '-'}, "
                            f"model={self.model}): the prompt was blocked "
                            "and no content was generated. Do not retry "
                            "or fall back to another provider for this topic."
                        )

                    text = ""
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "")
                    if not text:
                        # No block reason, but also no content: fail
                        # loudly instead of returning an empty "success"
                        # that only dies later in JSON parsing.
                        raise ValueError(
                            f"Gemini returned an empty response (no text "
                            f"in candidates, model={self.model})."
                        )

                    return LLMResponse(
                        content=text,
                        provider="gemini",
                        model=self.model,
                    )
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning(f"Gemini request failed using key [{mask_secret(api_key)}]: {exc}")
                resp_obj = getattr(exc, 'response', None)
                status = getattr(resp_obj, 'status_code', None)
                mark_gemini_key_failed(api_key, error_status=status if isinstance(status, int) else 500)
                if isinstance(status, int) and status >= 500:
                    server_error_statuses.append(status)
                last_err = exc
                continue

        if last_err is not None:
            if server_error_statuses and len(server_error_statuses) >= len(keys):
                # Every key failed with a 5xx: the backend is down,
                # not the request — skip tenacity's retry (it would
                # only re-run the same failing keys) and let the
                # fallback chain move to the next provider promptly.
                raise GeminiOverloadedError(
                    f"Gemini overloaded: all {len(keys)} key(s) failed "
                    f"with server errors {server_error_statuses} "
                    f"(model={self.model})."
                )
            raise last_err
        raise RuntimeError("All Gemini API keys in pool failed.")


    def _call_bedrock(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Type[T]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Call AWS Bedrock Claude models via boto3 bedrock-runtime."""
        aws_key = _get_secret_optional("AWS_ACCESS_KEY_ID")
        aws_secret = _get_secret_optional("AWS_SECRET_ACCESS_KEY")

        try:
            import boto3
            session_kwargs = {"region_name": "us-east-1"}
            if aws_key and aws_secret:
                session_kwargs["aws_access_key_id"] = aws_key
                session_kwargs["aws_secret_access_key"] = aws_secret
            
            client = boto3.client("bedrock-runtime", **session_kwargs)

            # Map model name
            model_id = self.model
            if "claude-3-5-haiku" in self.model:
                model_id = "anthropic.claude-3-5-haiku-20241022-v1:0"
            elif "claude-3-5-sonnet" in self.model:
                model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"

            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "temperature": temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }

            resp = client.invoke_model(
                modelId=model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            response_body = json.loads(resp["body"].read().decode("utf-8"))
            content = response_body.get("content", [{}])[0].get("text", "")

            return LLMResponse(
                content=content,
                provider="bedrock",
                model=self.model,
            )
        except Exception as e:
            # PRODUCTION PATCH (P0 #1): fixed the misleading log line only.
            # This method already re-raised; it never returned a fallback.
            logger.warning(f"Bedrock invocation failed ({e}). Raising.")
            raise

    def _call_openrouter(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Type[T]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Call OpenRouter API."""
        api_key = _get_secret_optional("OPENROUTER_KEY")
        if not api_key:
            logger.warning("OPENROUTER_KEY not configured. Raising error.")
            raise ValueError("OPENROUTER_KEY is required for OpenRouter provider")

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/alphaframehq/yt-agent",
            "X-Title": "TubePilot AI",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }

        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            api_error = data.get("error") if isinstance(data, dict) else None
            if api_error:
                raise ValueError(f"OpenRouter API error: {api_error}")
            choices = data.get("choices") or []
            content = None
            if choices:
                content = (choices[0].get("message") or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                # Run #17: OpenRouter returned content=None and the
                # LLMResponse(content=None) pydantic error crashed
                # the call instead of failing this attempt over to
                # the next provider in the chain.
                raise ValueError(
                    "OpenRouter returned no content (content=None or "
                    "empty); treating as a failed attempt."
                )

            return LLMResponse(
                content=content,
                provider="openrouter",
                model=self.model,
            )


def call_with_fallback(
    chain: List[List[str]],
    system_prompt: str,
    user_prompt: str,
    schema: Optional[Type[T]] = None,
    max_repair_attempts: int = 2,
) -> Any:
    """Execute LLM call across a fallback chain e.g. [['bedrock', 'claude-3-5-haiku'], ['gemini', 'gemini-3.8-flash']]."""
    last_error = None
    for provider_name, model_name in chain:
        try:
            logger.info(f"Attempting LLM call via {provider_name}/{model_name}")
            provider = LLMProvider(provider_name, model_name)
            if schema is not None:
                return provider.call_structured(
                    system_prompt, user_prompt, schema, max_repair_attempts=max_repair_attempts
                )
            else:
                return provider.call(system_prompt, user_prompt)
        except SafetyBlockedError:
            # A safety refusal is final for this prompt: do NOT try
            # the next provider (that only burns the chain asking a
            # looser model to answer a blocked prompt). Propagate so
            # the caller can mark the topic failed and move on.
            raise
        except Exception as e:
            logger.warning(f"Provider {provider_name}/{model_name} failed: {e}. Trying next in chain...")
            last_error = e

    raise RuntimeError(f"All LLM providers in fallback chain failed. Last error: {last_error}")
