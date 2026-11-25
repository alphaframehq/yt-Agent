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
    get_secret,
    get_gemini_pool_keys,
    mark_gemini_key_failed,
    mask_secret,
)

T = TypeVar("T", bound=BaseModel)

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "call_with_fallback",
]


class LLMResponse(BaseModel):
    """Normalized response from an LLM call."""
    content: str
    parsed: Optional[Any] = None
    provider: str
    model: str
    tokens_used: int = 0
    latency_ms: int = 0


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
            
            raw_text = res.content.strip()
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
                last_error = str(e)
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

                    resp.raise_for_status()
                    data = resp.json()

                    text = ""
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "")

                    return LLMResponse(
                        content=text,
                        provider="gemini",
                        model=self.model,
                    )
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning(f"Gemini request failed using key [{mask_secret(api_key)}]: {exc}")
                mark_gemini_key_failed(api_key, error_status=getattr(getattr(exc, 'response', None), 'status_code', 500))
                last_err = exc
                continue

        if last_err is not None:
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
        aws_key = get_secret("AWS_ACCESS_KEY_ID")
        aws_secret = get_secret("AWS_SECRET_ACCESS_KEY")

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
        api_key = get_secret("OPENROUTER_KEY")
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
            content = data["choices"][0]["message"]["content"]

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
    """Execute LLM call across a fallback chain e.g. [['bedrock', 'claude-3-5-haiku'], ['gemini', 'gemini-2.0-flash']]."""
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
        except Exception as e:
            logger.warning(f"Provider {provider_name}/{model_name} failed: {e}. Trying next in chain...")
            last_error = e

    raise RuntimeError(f"All LLM providers in fallback chain failed. Last error: {last_error}")
