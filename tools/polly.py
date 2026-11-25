# LOCAL PRODUCTION PATCH — 2026-11-25 — Applied via PR #1 (branch fix/production-mocks), 2026-11-25.
# Source: alphaframehq/yt-Agent @ main c39f42541b8e784dcf41c09583dc57227be07667
# Fixes audit P0 #8: synthesize_speech() now RAISES on any Polly failure
# (missing/invalid AWS keys, boto3 missing, network, Polly error, including
# after the Neural -> Standard fallback) instead of returning a 1-second
# silence stub as success. _generate_mock_audio() is gated behind an
# explicit ALLOW_DEMO_MODE env flag (default off) and raises otherwise.
# Word-count-estimated durations are no longer returned as real durations:
# _estimate_or_read_duration() measures the real MP3 length via mutagen
# and raises if it cannot.
"""AWS Polly Text-to-Speech tool for TubePilot AI.

Voice options:
- Neural: Joanna (US), Matthew (US), Kajal (IN)
- Standard fallback: Aditi, Raveena

Features:
- Structured SSML formatting with pacing optimization
- Captures actual audio duration in seconds
- Deterministic audio caching in ./cache/audio/
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Tuple
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from tools.secrets import get_secret

__all__ = [
    "synthesize_speech",
    "format_ssml",
]

CACHE_DIR = Path("cache/audio")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _demo_mode_enabled() -> bool:
    """True only when the caller explicitly opts into demo stubs.

    Default off. Production must never set ALLOW_DEMO_MODE.
    """
    return os.environ.get("ALLOW_DEMO_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_cache_path(text: str, voice: str) -> Path:
    h = hashlib.md5(f"{voice}:{text}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.mp3"


def format_ssml(text: str) -> str:
    """Format clean narration text with SSML prosody rate and trailing pause."""
    # Sanitize XML special characters
    clean = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    return f'<speak><prosody rate="+5%">{clean}</prosody><break time="800ms"/></speak>'


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def synthesize_speech(
    text: str,
    voice: str = "Joanna",
    engine: str = "neural",
) -> Tuple[str, float]:
    """Synthesize speech to mp3 and return (audio_path, duration_seconds).

    Raises on any Polly failure (missing/invalid AWS keys, boto3 missing,
    network error, Polly error — including after the Neural -> Standard
    fallback). Never returns silent/stub audio as success in production.
    The returned duration is the measured length of the real MP3 file.
    """
    cache_path = _get_cache_path(text, voice)

    # Check cache
    if cache_path.exists() and cache_path.stat().st_size > 0:
        duration = _estimate_or_read_duration(cache_path, text)
        return str(cache_path.resolve()), duration

    aws_key = get_secret("AWS_ACCESS_KEY_ID")
    aws_secret = get_secret("AWS_SECRET_ACCESS_KEY")
    if not aws_key or not aws_secret:
        raise RuntimeError(
            "AWS Polly credentials are not configured: "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are missing. "
            "Set them (via the secrets store / environment) — silent "
            "stub audio is no longer generated as a fallback."
        )
    ssml_text = format_ssml(text)

    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "boto3 is not installed, so AWS Polly cannot be called. "
            "Install boto3 and configure AWS credentials."
        ) from e

    try:
        session_kwargs = {"region_name": "us-east-1"}
        if aws_key and aws_secret:
            session_kwargs["aws_access_key_id"] = aws_key
            session_kwargs["aws_secret_access_key"] = aws_secret

        client = boto3.client("polly", **session_kwargs)

        try:
            # 1. Attempt Neural Engine
            resp = client.synthesize_speech(
                Engine=engine,
                OutputFormat="mp3",
                SampleRate="24000",
                Text=ssml_text,
                TextType="ssml",
                VoiceId=voice,
            )
        except Exception as ne:
            logger.warning(f"Polly Neural voice '{voice}' failed ({ne}). Trying Standard engine fallback...")
            # 2. Fallback to Standard Engine — if this also fails, the
            # exception below propagates; there is no silent-audio fallback.
            fallback_voice = "Aditi" if voice == "Kajal" else "Joanna"
            resp = client.synthesize_speech(
                Engine="standard",
                OutputFormat="mp3",
                SampleRate="16000",
                Text=ssml_text,
                TextType="ssml",
                VoiceId=fallback_voice,
            )

        if "AudioStream" not in resp:
            raise RuntimeError("AWS Polly returned no AudioStream in its response.")
        data = resp["AudioStream"].read()
        if not data:
            raise RuntimeError("AWS Polly returned an empty AudioStream.")
        cache_path.write_bytes(data)
        duration = _estimate_or_read_duration(cache_path, text)
        return str(cache_path.resolve()), duration

    except Exception as e:
        # Production behaviour: fail loudly. Demo stubs are only allowed
        # behind the explicit ALLOW_DEMO_MODE flag (default off).
        if _demo_mode_enabled():
            logger.warning(
                f"AWS Polly call failed ({e}). ALLOW_DEMO_MODE is set: "
                "generating local mock audio for dev only."
            )
            return _generate_mock_audio(cache_path, text)
        logger.error(f"AWS Polly call failed ({e}). Raising instead of returning stub audio.")
        raise

def _estimate_or_read_duration(path: Path, text: str) -> float:
    """Measure the real duration of the MP3 at *path*.

    Uses mutagen to read the actual audio length. Raises if the length
    cannot be measured — a word-count estimate (e.g. ~150 wpm) is NOT a
    real duration and is never returned as one.
    """
    try:
        # Check if mutagen is available
        import mutagen.mp3
        audio = mutagen.mp3.MP3(str(path))
        if audio.info and audio.info.length > 0:
            return round(audio.info.length, 2)
    except Exception as e:
        raise RuntimeError(
            f"Could not measure real audio duration of {path}: {e}. "
            "Install mutagen and ensure the file is a valid MP3; "
            "word-count-estimated durations are not returned as real."
        ) from e

    raise RuntimeError(
        f"Could not measure real audio duration of {path}: "
        "mutagen reported no usable length. Word-count-estimated "
        "durations are not returned as real."
    )


def _generate_mock_audio(dest_path: Path, text: str) -> Tuple[str, float]:
    """Demo-only: generate a minimal silent MP3 stub.

    Raises unless ALLOW_DEMO_MODE is explicitly enabled. Production code
    must never call this as a success path — synthesize_speech() raises
    on Polly failure instead.
    """
    if not _demo_mode_enabled():
        raise RuntimeError(
            "Mock/stub audio is disabled: set ALLOW_DEMO_MODE=1 explicitly "
            "for local dev only. Production synthesize_speech() must raise "
            "on Polly failure instead of returning silent audio."
        )
    # Demo-only duration estimate (~150 wpm -> 2.5 words/sec + 0.8s break).
    # This is an estimate for a dev stub only, never a real duration.
    word_count = len(text.split())
    duration = max(round((word_count / 2.5) + 0.8, 2), 2.0)
    # 1 second silence MPEG frame stub
    stub_mp3_header = bytes.fromhex(
        "fffb906400000000000000000000000000000000000000000000000000000000"
    )
    dest_path.write_bytes(stub_mp3_header * int(max(duration, 1)))
    logger.warning(f"DEMO MODE: wrote silent stub audio to {dest_path} (not real speech).")
    return str(dest_path.resolve()), duration
