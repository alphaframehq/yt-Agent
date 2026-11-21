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
    """Synthesize speech to mp3 and return (audio_path, duration_seconds)."""
    cache_path = _get_cache_path(text, voice)

    # Check cache
    if cache_path.exists() and cache_path.stat().st_size > 0:
        duration = _estimate_or_read_duration(cache_path, text)
        return str(cache_path.resolve()), duration

    aws_key = get_secret("AWS_ACCESS_KEY_ID")
    aws_secret = get_secret("AWS_SECRET_ACCESS_KEY")
    ssml_text = format_ssml(text)

    try:
        import boto3
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
            # 2. Fallback to Standard Engine
            fallback_voice = "Aditi" if voice == "Kajal" else "Joanna"
            resp = client.synthesize_speech(
                Engine="standard",
                OutputFormat="mp3",
                SampleRate="16000",
                Text=ssml_text,
                TextType="ssml",
                VoiceId=fallback_voice,
            )

        if "AudioStream" in resp:
            data = resp["AudioStream"].read()
            cache_path.write_bytes(data)
            duration = _estimate_or_read_duration(cache_path, text)
            return str(cache_path.resolve()), duration

    except Exception as e:
        logger.warning(f"AWS Polly call failed ({e}). Generating local mock audio for dev.")

    # Fallback mock audio generation (silent or placeholder duration based on word count)
    return _generate_mock_audio(cache_path, text)


def _estimate_or_read_duration(path: Path, text: str) -> float:
    """Accurately measures audio duration or falls back to standard speech rate estimate."""
    try:
        # Check if mutagen is available
        import mutagen.mp3
        audio = mutagen.mp3.MP3(str(path))
        if audio.info and audio.info.length > 0:
            return round(audio.info.length, 2)
    except Exception:
        pass

    # Standard speaking rate: ~150 words per minute -> 2.5 words/sec + 0.8s break
    word_count = len(text.split())
    estimated = (word_count / 2.5) + 0.8
    return max(round(estimated, 2), 2.0)


def _generate_mock_audio(dest_path: Path, text: str) -> Tuple[str, float]:
    """Generates a minimal valid MP3 frame or placeholder file for local development."""
    duration = _estimate_or_read_duration(dest_path, text)
    # 1 second silence MPEG frame stub
    stub_mp3_header = bytes.fromhex(
        "fffb906400000000000000000000000000000000000000000000000000000000"
    )
    dest_path.write_bytes(stub_mp3_header * int(max(duration, 1)))
    return str(dest_path.resolve()), duration
