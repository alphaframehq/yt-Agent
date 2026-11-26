"""Text-to-Speech (TTS) Agent for TubePilot AI.

PRIMARY provider (26 Nov 2026 decision — see tools/gemini_tts.py):
Gemini TTS (free tier, via GEMINI_KEY), because AWS Polly is
unusable — no AWS keys exist anywhere, so tools/polly.py raises on
every call and no video could ever assemble. The user was offered
the choice and has not answered; this decision unblocks the
pipeline with a key that already exists in GitHub repo secrets.
The user can overrule it later.

Provider selection (env TTS_PROVIDER, default "gemini"):
- "gemini" (default): Gemini TTS first. If it fails AND AWS Polly
  keys exist, Polly is tried once as a fallback; if Polly also
  fails (or no AWS keys exist) the ORIGINAL Gemini error is raised
  — fail loud, never silent/stub audio, never a masked error.
- "polly": Polly only (previous behaviour; raises without AWS keys).

Voice names come from config ("voice", default "Joanna"); both
providers accept the Polly-style names — tools/gemini_tts maps
Joanna -> Kore, Matthew -> Charon, Kajal -> Aoede.

Feeds accurate audio durations back into the scene timing model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from config_manager import get_config
from models.script import SceneInfo, VideoScript
from tools.gemini_tts import synthesize_speech as synthesize_speech_gemini
from tools.polly import synthesize_speech as synthesize_speech_polly
from tools.secrets import _get_secret_optional

__all__ = [
    "TTSAgent",
    "synthesize_scene_audio",
    "synthesize_script_audio",
    "TTS_PROVIDER",
]


def _tts_provider() -> str:
    """Configured TTS provider: 'gemini' (default) or 'polly'."""
    return os.environ.get("TTS_PROVIDER", "").strip().lower() or "gemini"


# Backwards-compatible module constant (evaluated at import; use
# _tts_provider() for the live value).
TTS_PROVIDER = _tts_provider()


def _polly_keys_present() -> bool:
    """True only when both AWS Polly credentials are configured."""
    return bool(
        _get_secret_optional("AWS_ACCESS_KEY_ID")
        and _get_secret_optional("AWS_SECRET_ACCESS_KEY")
    )


def _synthesize(text: str, voice: str) -> Tuple[str, float]:
    """Synthesize *text* with the configured provider (+ Polly fallback).

    Gemini-first by default; Polly is tried once on Gemini failure
    only when AWS keys exist. Raises the original Gemini error when
    there is no usable fallback — never stub audio.
    """
    provider = _tts_provider()
    if provider == "polly":
        return synthesize_speech_polly(text, voice=voice, engine="neural")
    if provider != "gemini":
        raise ValueError(
            f"Unknown TTS_PROVIDER {provider!r}: expected 'gemini' or 'polly'."
        )
    try:
        return synthesize_speech_gemini(text, voice=voice)
    except Exception as gemini_err:
        if not _polly_keys_present():
            logger.error(
                f"Gemini TTS failed ({gemini_err}); no AWS Polly keys "
                "exist, so there is no fallback. Raising."
            )
            raise
        logger.warning(
            f"Gemini TTS failed ({gemini_err}); AWS Polly keys exist, "
            "trying Polly once as fallback."
        )
        try:
            return synthesize_speech_polly(text, voice=voice, engine="neural")
        except Exception as polly_err:
            logger.error(
                f"Polly fallback also failed ({polly_err}); raising the "
                "original Gemini TTS error."
            )
            raise gemini_err from polly_err


class TTSAgent:
    """Orchestrates scene-by-scene audio narration synthesis and timing alignment."""

    def __init__(self, voice: Optional[str] = None):
        self.voice = voice or get_config("voice") or "Joanna"

    def synthesize_scene(self, scene: SceneInfo) -> Tuple[str, float]:
        """Synthesize a single scene's narration text and return (audio_path, duration_seconds)."""
        logger.info(f"Synthesizing audio for Scene {scene.id} ({len(scene.narration.split())} words)")
        audio_path, duration = _synthesize(
            text=scene.narration,
            voice=self.voice,
        )
        return audio_path, duration

    def synthesize_script(self, script: VideoScript) -> List[Dict[str, Any]]:
        """Synthesize audio for every scene in the script and update scene durations."""
        results: List[Dict[str, Any]] = []
        total_audio_sec = 0.0

        for scene in script.scenes:
            audio_path, duration = self.synthesize_scene(scene)
            # Update scene duration with exact spoken audio duration
            scene.duration_sec = int(round(duration))
            total_audio_sec += duration

            results.append({
                "scene_id": scene.id,
                "audio_path": audio_path,
                "duration_sec": duration,
                "section": scene.section,
            })

        logger.info(f"Script TTS synthesis completed. Total duration: {total_audio_sec:.1f}s across {len(results)} scenes.")
        return results


def synthesize_scene_audio(text: str, voice: str = "Joanna") -> Tuple[str, float]:
    """Convenience functional interface for scene audio synthesis."""
    return _synthesize(text, voice=voice)


def synthesize_script_audio(script: VideoScript, voice: Optional[str] = None) -> List[Dict[str, Any]]:
    """Convenience functional interface for script audio synthesis."""
    return TTSAgent(voice=voice).synthesize_script(script)
