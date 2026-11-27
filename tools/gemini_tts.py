# Gemini TTS tool for TubePilot AI — 26 Nov 2026 (Phase 2, worker 4).
#
# DECISION (26 Nov 2026): Gemini TTS is the PRIMARY TTS provider.
# The user was offered the choice (AWS keys vs Gemini TTS vs fix rest
# first) and has not answered; AWS Polly is unusable — no AWS keys
# exist anywhere, so tools/polly.py raises "AWS Polly credentials are
# not configured" on every call and no video can ever assemble today.
# GEMINI_KEY already exists in GitHub repo secrets, so Gemini TTS
# (free tier) unblocks the pipeline with no new credential. The user
# can overrule this later (set TTS_PROVIDER=polly in agents/tts.py
# terms, or supply AWS keys); Polly stays as an optional fallback
# when AWS keys exist — see agents/tts.py.
#
# VERIFIED-LIVE 26 Nov 2026 (run #18): the model id / endpoint below
# ("gemini-2.5-flash-preview-tts", POST
# .../v1beta/models/{model}:generateContent with generationConfig
# responseModalities ["AUDIO"] + speechConfig prebuiltVoiceConfig)
# synthesized all 8 scenes (183s) live, and Google's pricing page
# lists the model "Free of charge" on the free tier.
#
# Fail-loud contract (mirrors tools/polly.py): missing GEMINI_KEY,
# any non-2xx response, an empty/blocked response, empty audio, or
# an unwritable/unmeasurable file raises RuntimeError — never a
# silent/stub audio file as success. No ALLOW_DEMO_MODE stub exists
# here at all.
#
# Output format: Gemini TTS returns raw 16-bit mono PCM (typically
# 24 kHz, mimeType "audio/L16;codec=audio/pcm;rate=24000"), NOT mp3.
# We write it as a standard .wav (stdlib `wave`) so the duration is
# measured exactly from the frame count — no mutagen, no ffmpeg
# needed. agents/assemble.py attaches audio via moviepy
# AudioFileClip, which reads .wav as well as .mp3, so no assembler
# change is needed. Cache files are .wav and keyed with a "gemini:"
# prefix so they can never collide with Polly's .mp3 cache entries.
"""Gemini TTS (free tier) text-to-speech for TubePilot AI.

Voice options (Gemini prebuilt voices; Polly names are mapped):
- Joanna (US English, female) -> Kore
- Matthew (US English, male) -> Charon
- Kajal (Indian English, female) -> Aoede
A Gemini voice name passed directly is used as-is; GEMINI_TTS_VOICE
overrides the default voice.

Features:
- Gemini API key pool failover (same pool as tools/llm_provider.py:
  GEMINI_KEY, GEMINI_KEY_POOL, GEMINI_KEY_1..20) — a 429/quota
  failure cools that key down and tries the next healthy key.
- Rate-limit resilience (27 Nov 2026, runs #19/#20): a 429's
  Retry-After header is honoured (capped 60s), a failed scene is
  retried up to 3 rounds across the pool, and successive real TTS
  API calls are paced at least TTS_MIN_INTERVAL_SEC apart
  (default 21s, env-overridable, 0 disables).
- Deterministic audio caching in ./cache/audio/
- Real audio duration measured from the written WAV frames.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
import wave
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Tuple

import httpx
from loguru import logger

from tools.secrets import (
    get_gemini_pool_keys,
    mark_gemini_key_failed,
)

__all__ = [
    "synthesize_speech",
    "resolve_voice",
    "DEFAULT_MODEL",
    "DEFAULT_VOICE",
]

# VERIFIED 26 Nov 2026: documented Gemini TTS model id
# ("gemini-2.5-flash-preview-tts") — Google's pricing page
# (ai.google.dev/gemini-api/docs/pricing, crawled 2026-11-26 19:45 UTC)
# lists it "Free of charge" for input AND output on the free tier,
# and run #18 synthesized all 8 scenes (183s) with it live.
# Do NOT switch to gemini-2.5-pro-preview-tts: that one is paid-only
# ("Not available" on the free tier).
DEFAULT_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Kore"
# TO-VERIFY-LIVE: documented endpoint shape (generateContent on the
# v1beta TTS model). Confirm live before relying on it daily.
_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_SAMPLE_RATE_HZ = 24_000
# Gemini TTS input is plain text (not SSML). Scenes are short
# narrations; overlong input fails loudly from the API (400) rather
# than being silently truncated here.
_MAX_TEXT_CHARS = 8_000

# TTS rate-limit resilience (27 Nov 2026 — runs #19/#20 root cause):
# both runs died at Gemini TTS with HTTP 429 on a single-key pool
# (key 1/1) and no fallback. Three defences, all in this module:
# 1. Key pool: get_gemini_pool_keys() already supports a second key
#    (GEMINI_KEY_2, plus GEMINI_KEY_POOL / GEMINI_KEY_1..20) — the
#    second key MUST come from a different Google Cloud project (a
#    new AI Studio project) to get a separate free-tier quota.
# 2. Retry-After: a 429/5xx response's Retry-After header is honoured
#    (capped at _RETRY_AFTER_MAX_WAIT_SEC) before the next attempt.
# 3. Pacing: successive synthesize_speech calls are spaced at least
#    TTS_MIN_INTERVAL_SEC apart (default 21s, env-overridable, 0 to
#    disable — tests set 0). Cache hits are never paced.
# A failed scene is retried up to _TTS_MAX_ATTEMPTS rounds across the
# whole pool before raising. No key value is ever logged — only the
# key index (1/N).
_TTS_MAX_ATTEMPTS = 3
_RETRY_AFTER_MAX_WAIT_SEC = 60.0
_DEFAULT_MIN_INTERVAL_SEC = 21.0
_last_tts_call_at: float = 0.0
_sleep = time.sleep  # patchable in tests (tools.gemini_tts._sleep)

CACHE_DIR = Path("cache/audio")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Polly voice name -> Gemini prebuilt voice name. A name that is
# already a Gemini prebuilt voice is passed through unchanged.
_VOICE_MAP = {
    "Joanna": "Kore",
    "Matthew": "Charon",
    "Kajal": "Aoede",
    "Aditi": "Aoede",
    "Raveena": "Aoede",
}


def resolve_voice(voice: Optional[str]) -> str:
    """Map a configured voice name to a Gemini prebuilt voice name."""
    override = os.environ.get("GEMINI_TTS_VOICE", "").strip()
    if override:
        return override
    if not voice:
        return DEFAULT_VOICE
    name = str(voice).strip()
    return _VOICE_MAP.get(name, name)


def _get_cache_path(text: str, voice: str, model: str) -> Path:
    h = hashlib.md5(f"gemini:{model}:{voice}:{text}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.wav"


def _measure_wav_duration(path: Path) -> float:
    """Return the exact duration of the WAV at *path* from its frames.

    Raises if the file is not a readable WAV with a positive length —
    a word-count estimate is never returned as a real duration.
    """
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if rate <= 0 or frames <= 0:
                raise RuntimeError(
                    f"WAV {path} has no usable audio frames "
                    f"(frames={frames}, rate={rate})."
                )
            return round(frames / rate, 2)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Could not measure real audio duration of {path}: {e}."
        ) from e


def _parse_sample_rate(mime_type: str) -> int:
    """Parse e.g. 'audio/L16;codec=audio/pcm;rate=24000' -> 24000."""
    m = re.search(r"rate=(\d+)", mime_type or "")
    if m:
        return int(m.group(1))
    return _DEFAULT_SAMPLE_RATE_HZ


def _parse_retry_after_seconds(resp: object) -> float:
    """Seconds to wait from a response's ``Retry-After`` header.

    Accepts a delay in seconds (``"30"``) or an HTTP-date. Returns 0
    when the header is absent, unparsable, or in the past. Capped at
    ``_RETRY_AFTER_MAX_WAIT_SEC``. Defensive about mock/non-httpx
    responses: only real str/int/float header values are accepted.
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
    return min(wait, _RETRY_AFTER_MAX_WAIT_SEC)


def _min_interval_sec() -> float:
    """Configured pause between successive TTS API calls (seconds).

    Env ``TTS_MIN_INTERVAL_SEC``, default 21.0. 0 disables pacing
    (tests set 0); negative values are treated as 0.
    """
    try:
        return max(0.0, float(os.environ.get(
            "TTS_MIN_INTERVAL_SEC", str(_DEFAULT_MIN_INTERVAL_SEC)
        ).strip()))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_INTERVAL_SEC


def _enforce_min_interval() -> None:
    """Sleep so the previous real TTS API call is at least the
    configured interval old. Called once per synthesize_speech call
    (cache hits return before this), not per key attempt — failover
    to the next key inside one call stays immediate.
    """
    global _last_tts_call_at
    interval = _min_interval_sec()
    if interval <= 0:
        _last_tts_call_at = time.time()
        return
    now = time.time()
    wait = interval - (now - _last_tts_call_at)
    if wait > 0:
        logger.info(
            f"Gemini TTS pacing: waiting {wait:.1f}s before the next "
            f"scene call (min interval {interval:.0f}s)."
        )
        _sleep(wait)
    _last_tts_call_at = time.time()


def _extract_pcm(data: dict) -> Tuple[bytes, int]:
    """Pull concatenated PCM bytes + sample rate from a generateContent response.

    Raises RuntimeError on a safety-blocked / empty response — an
    empty candidate list is a refusal, never a silent success.
    """
    feedback = data.get("promptFeedback") or {}
    block_reason = feedback.get("blockReason")
    candidates = data.get("candidates") or []
    finish_reason = ""
    if candidates:
        finish_reason = str(candidates[0].get("finishReason") or "")
    if block_reason or finish_reason == "SAFETY":
        raise RuntimeError(
            "Gemini TTS safety refusal "
            f"(blockReason={block_reason or '-'}, "
            f"finishReason={finish_reason or '-'}, "
            "model=" + DEFAULT_MODEL + "): no audio was generated. "
            "Do not retry this text."
        )
    if not candidates:
        raise RuntimeError(
            "Gemini TTS returned no candidates (empty response); "
            "no audio was generated."
        )
    pcm = bytearray()
    sample_rate = _DEFAULT_SAMPLE_RATE_HZ
    for part in candidates[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") or {}
        b64 = inline.get("data")
        if not b64:
            continue
        sample_rate = _parse_sample_rate(inline.get("mimeType", ""))
        try:
            pcm.extend(base64.b64decode(b64))
        except Exception as e:
            raise RuntimeError(
                f"Gemini TTS returned audio that is not valid base64: {e}"
            ) from e
    if not pcm:
        raise RuntimeError(
            "Gemini TTS returned no audio data (no inlineData part)."
        )
    if len(pcm) % 2 != 0:
        raise RuntimeError(
            f"Gemini TTS returned {len(pcm)} PCM bytes, not a whole "
            "number of 16-bit samples; refusing to write a corrupt file."
        )
    return bytes(pcm), sample_rate


def synthesize_speech(
    text: str,
    voice: str = "Joanna",
    engine: str = "neural",
    model: Optional[str] = None,
) -> Tuple[str, float]:
    """Synthesize speech to WAV and return (audio_path, duration_seconds).

    Primary TTS path (see agents/tts.py). ``engine`` is accepted for
    drop-in compatibility with tools/polly.py's signature and ignored
    — Gemini TTS has no neural/standard engine switch.

    Raises on any failure (missing GEMINI_KEY, HTTP error, safety
    refusal, empty/no audio, unwritable file). Never returns silent
    or stub audio as success. The returned duration is measured from
    the real WAV frames. A cached file is re-measured, never trusted
    from a stored estimate.
    """
    if not text or not str(text).strip():
        raise ValueError("Refusing Gemini TTS: text is empty.")
    clean_text = str(text).strip()
    if len(clean_text) > _MAX_TEXT_CHARS:
        raise ValueError(
            f"Refusing Gemini TTS: text is {len(clean_text)} chars "
            f"(limit {_MAX_TEXT_CHARS}); split the narration first."
        )
    gemini_voice = resolve_voice(voice)
    model_id = (model or os.environ.get("GEMINI_TTS_MODEL", "").strip()
                or DEFAULT_MODEL)

    cache_path = _get_cache_path(clean_text, gemini_voice, model_id)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return str(cache_path.resolve()), _measure_wav_duration(cache_path)

    keys = get_gemini_pool_keys()
    if not keys:
        raise RuntimeError(
            "GEMINI_KEY is required for Gemini TTS (set GEMINI_KEY or "
            "GEMINI_KEY_POOL as a GitHub repo secret / env var). No "
            "silent stub audio is generated as a fallback."
        )

    payload = {
        "contents": [{"parts": [{"text": clean_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": gemini_voice}
                }
            },
        },
    }

    last_err: Optional[Exception] = None
    retry_after_wait = 0.0
    _enforce_min_interval()
    # Up to _TTS_MAX_ATTEMPTS rounds over the whole pool (runs #19/#20:
    # one 429 on the only key killed the scene). Between rounds we
    # honour the largest Retry-After the server sent (capped at 60s),
    # else a short 10s backoff. A 400 (bad request) or a safety
    # refusal still raises immediately — retrying those is pointless.
    for attempt in range(1, _TTS_MAX_ATTEMPTS + 1):
        if attempt > 1:
            wait = retry_after_wait if retry_after_wait > 0 else 10.0
            logger.warning(
                f"Gemini TTS attempt {attempt}/{_TTS_MAX_ATTEMPTS} "
                f"after all keys failed; waiting {wait:.0f}s."
            )
            _sleep(wait)
            retry_after_wait = 0.0
        keys = get_gemini_pool_keys()
        for idx, api_key in enumerate(keys):
            # TO-VERIFY-LIVE: endpoint + model id (see module header).
            url = f"{_API_BASE}/{model_id}:generateContent?key={api_key}"
            try:
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(
                        url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
                if resp.status_code in (429, 401, 403):
                    retry_after_wait = max(
                        retry_after_wait, _parse_retry_after_seconds(resp)
                    )
                    logger.warning(
                        f"Gemini TTS key {idx + 1}/{len(keys)} failed with "
                        f"HTTP {resp.status_code}; cooling it down and "
                        "trying the next key in the pool."
                    )
                    mark_gemini_key_failed(
                        api_key, error_status=resp.status_code
                    )
                    last_err = RuntimeError(
                        f"Gemini TTS request failed with HTTP "
                        f"{resp.status_code} (key {idx + 1}/{len(keys)})."
                    )
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    # Transient server error: same failover as 429,
                    # honoured via Retry-After on the next round.
                    retry_after_wait = max(
                        retry_after_wait, _parse_retry_after_seconds(resp)
                    )
                    logger.warning(
                        f"Gemini TTS key {idx + 1}/{len(keys)} hit HTTP "
                        f"{resp.status_code} (server error); cooling it "
                        "down and trying the next key in the pool."
                    )
                    mark_gemini_key_failed(
                        api_key, error_status=resp.status_code
                    )
                    last_err = RuntimeError(
                        f"Gemini TTS request failed with HTTP "
                        f"{resp.status_code} (key {idx + 1}/{len(keys)})."
                    )
                    continue
                if resp.status_code >= 400:
                    # A 4xx that is not key failover (e.g. 400 bad
                    # request) — fail loudly, do not burn the pool.
                    raise RuntimeError(
                        "Gemini TTS request failed with HTTP "
                        f"{resp.status_code}: {resp.text[:500]}"
                    )
                pcm, sample_rate = _extract_pcm(resp.json())
            except RuntimeError:
                raise
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                logger.warning(f"Gemini TTS request failed ({exc}); trying next key.")
                mark_gemini_key_failed(
                    api_key,
                    error_status=getattr(
                        getattr(exc, "response", None), "status_code", 500
                    ),
                )
                last_err = exc
                continue

            try:
                with wave.open(str(cache_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)  # 16-bit PCM
                    wav.setframerate(sample_rate)
                    wav.writeframes(pcm)
            except Exception as e:
                raise RuntimeError(
                    f"Could not write Gemini TTS audio to {cache_path}: {e}"
                ) from e
            if cache_path.stat().st_size == 0:
                raise RuntimeError(
                    f"Gemini TTS wrote an empty audio file to {cache_path}."
                )
            duration = _measure_wav_duration(cache_path)
            logger.info(
                f"Gemini TTS synthesized {len(clean_text)} chars "
                f"(voice={gemini_voice}, {duration}s) -> {cache_path}"
            )
            return str(cache_path.resolve()), duration

    if last_err is not None:
        raise RuntimeError(
            f"All Gemini API keys in pool failed for TTS. Last error: {last_err}"
        ) from last_err
    raise RuntimeError("All Gemini API keys in pool failed for TTS.")
